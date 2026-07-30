from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.real_surgery_annotation import run_marlin2_policy02_batch as batch
from tools.real_surgery_annotation import run_marlin2_proposals


class Policy02PromptContractTest(unittest.TestCase):
    def test_all_queries_use_observable_boundary_criteria(self) -> None:
        self.assertEqual(
            {
                "implicit_tool_request",
                "scrub_nurse_to_surgeon",
                "mayo_stand_to_scrub_nurse",
                "surgeon_to_scrub_nurse",
                "scrub_nurse_to_mayo_stand",
                "surgeon_to_mayo_stand",
            },
            set(run_marlin2_proposals.MODEL_QUERIES),
        )
        request_prompt = " ".join(
            run_marlin2_proposals.MODEL_QUERIES["implicit_tool_request"]
        )
        self.assertIn("fully open", request_prompt)
        self.assertIn("facing upward", request_prompt)
        self.assertIn("empty", request_prompt)
        self.assertIn("before tool contact", request_prompt)
        self.assertIn("exclude reaching", request_prompt)

        handover_prompt = " ".join(
            run_marlin2_proposals.MODEL_QUERIES["scrub_nurse_to_surgeon"]
        )
        self.assertIn("secure control", handover_prompt)
        self.assertIn("exclude approach", handover_prompt)
        self.assertIn("first visible moment", handover_prompt)

        pickup_prompt = " ".join(
            run_marlin2_proposals.MODEL_QUERIES["mayo_stand_to_scrub_nurse"]
        )
        self.assertIn("lifted clear", pickup_prompt)
        self.assertIn("separates from", pickup_prompt)

        return_prompt = " ".join(
            run_marlin2_proposals.MODEL_QUERIES["surgeon_to_scrub_nurse"]
        )
        self.assertIn("stable control", return_prompt)
        self.assertIn("not merely", return_prompt)

        placement_prompt = " ".join(
            run_marlin2_proposals.MODEL_QUERIES["scrub_nurse_to_mayo_stand"]
        )
        self.assertIn("releases", placement_prompt)
        self.assertIn("settled", placement_prompt)

        direct_return_prompt = " ".join(
            run_marlin2_proposals.MODEL_QUERIES["surgeon_to_mayo_stand"]
        )
        self.assertIn("direct-return boundary", direct_return_prompt)
        self.assertIn("releases", direct_return_prompt)
        self.assertIn("settled", direct_return_prompt)
        self.assertIn("without an intervening", direct_return_prompt)


class Policy02JobPlanTest(unittest.TestCase):
    def test_build_jobs_has_exact_pass_contract_and_create_only_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = batch.build_jobs(
                case_ids=("0704_7",),
                annotation_root=root / "annotations",
                video_root=root / "videos",
                model=root / "model",
                model_revision="revision",
                python_executable=root / "venv/bin/python",
                runner=root / "runner.py",
            )

        self.assertEqual(2, len(jobs))
        by_name = {job.pass_spec.name: job for job in jobs}
        transcript = by_name["transcript"]
        self.assertEqual(1.25, transcript.pass_spec.clip_before_sec)
        self.assertEqual(4.25, transcript.pass_spec.clip_after_sec)
        self.assertEqual(
            (
                "implicit_tool_request",
                "scrub_nurse_to_surgeon",
            ),
            transcript.pass_spec.event_types,
        )
        scan = by_name["scan"]
        self.assertEqual(7.0, scan.pass_spec.clip_before_sec)
        self.assertEqual(7.0, scan.pass_spec.clip_after_sec)
        self.assertEqual(
            (
                "implicit_tool_request",
                "mayo_stand_to_scrub_nurse",
                "surgeon_to_scrub_nurse",
                "scrub_nurse_to_mayo_stand",
                "scrub_nurse_to_surgeon",
                "surgeon_to_mayo_stand",
            ),
            scan.pass_spec.event_types,
        )
        for job in jobs:
            self.assertIn(".policy02.v1.", job.output.name)
            self.assertNotIn("initial", job.output.name)
            self.assertIn("--skip-caption", job.command)
            self.assertEqual("cuda", job.command[job.command.index("--device") + 1])
            self.assertEqual(
                str((root / "venv/bin/python").absolute()),
                job.command[0],
            )

    def test_parse_case_ids_rejects_out_of_scope_or_duplicates(self) -> None:
        self.assertEqual(
            ("0704_7", "0704_17"),
            batch.parse_case_ids("0704_7,0704_17"),
        )
        with self.assertRaises(Exception):
            batch.parse_case_ids("0704_6")
        with self.assertRaises(Exception):
            batch.parse_case_ids("0704_7,0704_7")

    def test_subset_batch_report_cannot_block_full_batch_report(self) -> None:
        root = Path("/tmp/annotations")
        self.assertNotEqual(
            batch.canonical_batch_report(root, ("0704_7",)),
            batch.canonical_batch_report(root, batch.TARGET_CASE_IDS),
        )
        self.assertTrue(
            batch.canonical_batch_report(root, ("0704_7",))
            .name
            .endswith(".batch.7.json")
        )

    def test_cli_hard_rejects_more_than_two_workers(self) -> None:
        argv = ["run_marlin2_policy02_batch.py", "--max-workers", "3"]
        with (
            mock.patch.object(sys, "argv", argv),
            self.assertRaises(SystemExit) as raised,
        ):
            batch.main()
        self.assertEqual(2, raised.exception.code)


class Policy02ConcurrencyTest(unittest.TestCase):
    def test_executor_never_runs_more_than_two_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = batch.build_jobs(
                case_ids=("0704_7", "0704_8", "0704_9"),
                annotation_root=root / "annotations",
                video_root=root / "videos",
                model=root / "model",
                model_revision="revision",
                python_executable=root / "python",
                runner=root / "runner.py",
            )
            lock = threading.Lock()
            active = 0
            peak = 0

            def fake_run_job(
                job: batch.Job,
                **_kwargs: object,
            ) -> dict[str, object]:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return {"job_id": job.job_id, "status": "completed"}

            with mock.patch.object(batch, "run_job", side_effect=fake_run_job):
                records = batch.execute_pending_jobs(
                    jobs,
                    max_workers=2,
                    workspace_root=root,
                    model=root / "model",
                    model_revision="revision",
                )

        self.assertEqual(2, peak)
        self.assertEqual(len(jobs), len(records))
        with self.assertRaisesRegex(ValueError, "one or two"):
            batch.execute_pending_jobs(
                [],
                max_workers=3,
                workspace_root=Path("/tmp"),
                model=Path("/tmp/model"),
                model_revision="revision",
            )


class Policy02ExecutionPreflightTest(unittest.TestCase):
    @staticmethod
    def write_process(
        proc_root: Path,
        pid: int,
        argv: list[str],
    ) -> None:
        process_root = proc_root / str(pid)
        process_root.mkdir(parents=True)
        (process_root / "cmdline").write_bytes(
            b"\0".join(argument.encode("utf-8") for argument in argv) + b"\0"
        )

    def test_process_scan_matches_runner_token_without_shell_false_positive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            self.write_process(
                proc_root,
                100,
                [
                    "/venv/bin/python",
                    "/workspace/tools/run_marlin2_proposals.py",
                    "--case-id",
                    "0704_7",
                ],
            )
            self.write_process(
                proc_root,
                101,
                [
                    "/bin/bash",
                    "-lc",
                    "rg run_marlin2_proposals.py /workspace",
                ],
            )
            self.write_process(
                proc_root,
                102,
                [
                    "/venv/bin/python",
                    "/workspace/tools/run_marlin2_policy02_batch.py",
                ],
            )
            matches = batch.find_existing_marlin_runners(
                proc_root=proc_root,
                excluded_pids={102},
            )

        self.assertEqual([100], [item["pid"] for item in matches])

    def test_vram_preflight_passes_and_fails_closed_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            completed = types.SimpleNamespace(
                returncode=0,
                stdout="25322\n",
                stderr="",
            )
            with mock.patch.object(
                batch.subprocess,
                "run",
                return_value=completed,
            ) as mocked:
                evidence = batch.run_execution_preflight(
                    min_free_vram_mib=20_000,
                    proc_root=proc_root,
                )
            self.assertEqual("passed", evidence["status"])
            self.assertEqual(25_322, evidence["free_vram_mib"])
            self.assertEqual(
                "nvidia-smi",
                mocked.call_args.args[0][0],
            )

            low_memory = types.SimpleNamespace(
                returncode=0,
                stdout="19999\n",
                stderr="",
            )
            with (
                mock.patch.object(
                    batch.subprocess,
                    "run",
                    return_value=low_memory,
                ),
                self.assertRaisesRegex(
                    batch.PreflightFailure,
                    "below required",
                ) as raised,
            ):
                batch.run_execution_preflight(
                    min_free_vram_mib=20_000,
                    proc_root=proc_root,
                )
            self.assertEqual(19_999, raised.exception.evidence["free_vram_mib"])

    def test_preflight_rejects_existing_marlin_child_without_killing_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            self.write_process(
                proc_root,
                200,
                [
                    "/venv/bin/python",
                    "/workspace/tools/run_marlin2_proposals.py",
                ],
            )
            completed = types.SimpleNamespace(
                returncode=0,
                stdout="30000\n",
                stderr="",
            )
            with (
                mock.patch.object(
                    batch.subprocess,
                    "run",
                    return_value=completed,
                ),
                self.assertRaisesRegex(
                    batch.PreflightFailure,
                    "already active",
                ) as raised,
            ):
                batch.run_execution_preflight(
                    min_free_vram_mib=20_000,
                    proc_root=proc_root,
                )

        self.assertEqual(
            [200],
            [
                item["pid"]
                for item in raised.exception.evidence[
                    "conflicting_marlin_processes"
                ]
            ],
        )

    def test_batch_report_records_vram_threshold_and_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "runner.py"
            runner.write_text("# fixture\n", encoding="utf-8")
            evidence = {
                "status": "passed",
                "free_vram_mib": 25_000,
                "min_free_vram_mib": 20_000,
            }
            report = batch.make_batch_report(
                status="completed",
                max_workers=2,
                case_ids=("0704_7",),
                model=root / "model",
                model_revision="revision",
                model_manifest_sha256="a" * 64,
                model_manifest_file_count=1,
                runner=runner,
                min_free_vram_mib=20_000,
                execution_preflight=evidence,
                jobs=[],
                started_at="2026-07-29T00:00:00Z",
                generated_at="2026-07-29T00:00:01Z",
            )

        self.assertEqual(20_000, report["settings"]["min_free_vram_mib"])
        self.assertEqual(evidence, report["execution_preflight"])


class Policy02ResumeSafetyTest(unittest.TestCase):
    @staticmethod
    def make_completed_pair(root: Path) -> tuple[batch.Job, Path, str]:
        annotation_root = root / "annotations"
        video_root = root / "videos"
        model = root / "model"
        revision = "fixture-revision"
        jobs = batch.build_jobs(
            case_ids=("0704_7",),
            annotation_root=annotation_root,
            video_root=video_root,
            model=model,
            model_revision=revision,
            python_executable=root / "python",
            runner=root / "runner.py",
        )
        job = jobs[0]
        for path, contents in (
            (job.video, b"video"),
            (job.timeline, b"timeline"),
            (job.anchors, b"anchors"),
            (job.output, b'{"proposal":true}\n'),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        report = {
            "schema": "taskplanner.marlin2_proposal_run.v1",
            "status": "completed",
            "case_id": job.case_id,
            "model": {
                "revision": revision,
                "local_path": str(model.resolve()),
            },
            "inputs": {
                "video": str(job.video.resolve()),
                "video_sha256": batch.sha256_file(job.video),
                "timeline": str(job.timeline.resolve()),
                "timeline_sha256": batch.sha256_file(job.timeline),
                "anchors": str(job.anchors.resolve()),
                "anchors_sha256": batch.sha256_file(job.anchors),
            },
            "settings": {
                "query_policy_id": batch.MODEL_QUERY_POLICY_ID,
                "query_prompt_sha256": job.prompt_sha256,
                "event_types": list(job.pass_spec.event_types),
                "clip_before_sec": job.pass_spec.clip_before_sec,
                "clip_after_sec": job.pass_spec.clip_after_sec,
                "skip_caption": True,
            },
            "output": str(job.output.resolve()),
            "output_sha256": batch.sha256_file(job.output),
        }
        job.report.parent.mkdir(parents=True, exist_ok=True)
        job.report.write_text(json.dumps(report), encoding="utf-8")
        return job, model, revision

    def test_complete_pair_is_reused_and_tampering_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, model, revision = self.make_completed_pair(Path(temporary))
            status, report, error = batch.inspect_job(
                job,
                model=model,
                model_revision=revision,
            )
            self.assertEqual("reused_completed", status)
            self.assertIsNotNone(report)
            self.assertIsNone(error)

            job.output.write_text("tampered", encoding="utf-8")
            status, report, error = batch.inspect_job(
                job,
                model=model,
                model_revision=revision,
            )
            self.assertEqual("blocked_invalid_existing_artifact", status)
            self.assertIsNone(report)
            self.assertIn("hash mismatch", error)

    def test_lone_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = batch.build_jobs(
                case_ids=("0704_7",),
                annotation_root=root / "annotations",
                video_root=root / "videos",
                model=root / "model",
                model_revision="revision",
                python_executable=root / "python",
                runner=root / "runner.py",
            )
            job = jobs[0]
            job.output.parent.mkdir(parents=True, exist_ok=True)
            job.output.write_text("preserve-me", encoding="utf-8")
            status, report, error = batch.inspect_job(
                job,
                model=root / "model",
                model_revision="revision",
            )

        self.assertEqual("blocked_partial_artifact", status)
        self.assertIsNone(report)
        self.assertIn("create-only", error)

    def test_reuse_rejects_changed_source_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, model, revision = self.make_completed_pair(Path(temporary))
            job.timeline.write_text("changed-timeline", encoding="utf-8")
            status, report, error = batch.inspect_job(
                job,
                model=model,
                model_revision=revision,
            )

        self.assertEqual("blocked_invalid_existing_artifact", status)
        self.assertIsNone(report)
        self.assertIn("input content hash mismatch", error)


if __name__ == "__main__":
    unittest.main()
