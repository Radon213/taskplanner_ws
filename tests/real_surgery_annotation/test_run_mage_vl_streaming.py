from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.real_surgery_annotation import run_mage_vl_streaming


class MageVlStreamingParserTest(unittest.TestCase):
    def test_parses_official_gate_lines_and_multiline_response(self) -> None:
        stdout = "\n".join(
            [
                "loader diagnostic",
                "[t=0-8.0s] gate=silence (p=1.9e-1)",
                "[t=8.000-16.5s] gate=response (p=.55) -> Instrument moves.",
                "The hands then separate.",
                "[t=16.5-24s] skip (segment unusable)",
            ]
        )

        records, unparsed = run_mage_vl_streaming.parse_streaming_stdout(stdout)

        self.assertEqual(3, len(records))
        self.assertEqual(["loader diagnostic"], unparsed)
        self.assertEqual("silence", records[0]["gate_decision"])
        self.assertAlmostEqual(0.19, records[0]["gate_probability"])
        self.assertEqual(
            "Instrument moves.\nThe hands then separate.",
            records[1]["response_text"],
        )
        self.assertEqual("skipped_unusable", records[2]["segment_status"])

    def test_invalid_probability_is_not_promoted_to_evidence(self) -> None:
        records, unparsed = run_mage_vl_streaming.parse_streaming_stdout(
            "[t=0.0-8.0s] gate=response (p=1.2) -> impossible"
        )
        self.assertEqual([], records)
        self.assertEqual(1, len(unparsed))

    def test_parses_first_segment_after_noninteractive_prompt(self) -> None:
        records, unparsed = run_mage_vl_streaming.parse_streaming_stdout(
            "Do you wish to run the custom code? [y/N] "
            "[t=0.0-8.0s] gate=response (p=0.55) -> Motion."
        )
        self.assertEqual(1, len(records))
        self.assertEqual(0.55, records[0]["gate_probability"])
        self.assertEqual(
            ["Do you wish to run the custom code? [y/N]"],
            unparsed,
        )

    def test_evidence_schema_has_no_observable_event_authority(self) -> None:
        records = run_mage_vl_streaming.make_evidence_records(
            [
                {
                    "segment_start_sec": 0.0,
                    "segment_end_sec": 8.0,
                    "segment_status": "gate_evaluated",
                    "gate_decision": "response",
                    "gate_probability": 0.6,
                    "response_text": "A possible motion is visible.",
                    "raw_stdout": (
                        "[t=0.0-8.0s] gate=response (p=0.60) -> "
                        "A possible motion is visible."
                    ),
                }
            ],
            case_id="0704_5",
            video=Path("/tmp/fixture.mp4"),
            gate_threshold=0.5,
        )
        self.assertEqual(
            "non_authoritative_model_evidence",
            records[0]["authority"],
        )
        self.assertFalse(records[0]["may_publish_ground_truth"])
        self.assertFalse(records[0]["observable_event_created"])
        for forbidden in ("event_type", "time_sec", "tool", "from", "to"):
            self.assertNotIn(forbidden, records[0])


class MageVlStreamingRunnerTest(unittest.TestCase):
    def _fixture_paths(self, root: Path) -> tuple[Path, Path]:
        video = root / "input.mp4"
        video.write_bytes(b"fixture")
        mage_repo = root / "mage"
        script = mage_repo / "mage_vl" / "inference_streaming.py"
        script.parent.mkdir(parents=True)
        script.write_text("# fixture official script\n", encoding="utf-8")
        return video, mage_repo

    def test_main_runs_official_script_and_writes_evidence_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video, mage_repo = self._fixture_paths(root)
            output = root / "evidence.jsonl"
            report_path = root / "report.json"
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    "[t=0.0-8.0s] gate=silence (p=0.19)\n"
                    "[t=8.0-16.0s] gate=response (p=0.55) -> Motion.\n"
                ),
                stderr="",
            )

            with mock.patch.object(
                run_mage_vl_streaming.subprocess,
                "run",
                return_value=completed,
            ) as run_mock:
                with contextlib.redirect_stdout(io.StringIO()):
                    return_code = run_mage_vl_streaming.main(
                        [
                            "--video",
                            str(video),
                            "--case",
                            "0704_5",
                            "--mage-repo",
                            str(mage_repo),
                            "--threshold",
                            "0.5",
                            "--segment-sec",
                            "8",
                            "--max-segments",
                            "2",
                            "--output",
                            str(output),
                            "--report",
                            str(report_path),
                        ]
                    )

            self.assertEqual(0, return_code)
            command = run_mock.call_args.args[0]
            self.assertEqual(
                str((mage_repo / "mage_vl/inference_streaming.py").resolve()),
                command[1],
            )
            self.assertIn("--video_backend", command)
            self.assertIn("codec", command)
            self.assertIn("--attn_impl", command)
            self.assertIn("sdpa", command)
            self.assertNotIn("--api-key", command)

            evidence = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(2, len(evidence))
            self.assertEqual(0, report["ground_truth_event_count"])
            self.assertEqual(0, report["observable_event_count"])
            self.assertFalse(report["may_publish_ground_truth"])
            self.assertEqual(0, report["return_code"])
            self.assertEqual("sdpa", report["attention_implementation"])
            self.assertIn("elapsed_sec", report)
            self.assertIn("model_environment", report)
            self.assertEqual(completed.stdout, report["raw_stdout"])

    def test_command_preserves_virtual_environment_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video, mage_repo = self._fixture_paths(root)
            args = run_mage_vl_streaming.parse_args(
                [
                    "--video",
                    str(video),
                    "--case",
                    "0704_5",
                    "--mage-repo",
                    str(mage_repo),
                    "--output",
                    str(root / "evidence.jsonl"),
                    "--report",
                    str(root / "report.json"),
                ]
            )
            script = run_mage_vl_streaming.resolve_official_script(mage_repo)
            with mock.patch.object(
                run_mage_vl_streaming.sys,
                "executable",
                "/tmp/example-venv/bin/python",
            ):
                command = run_mage_vl_streaming.build_command(args, script)

            self.assertEqual("/tmp/example-venv/bin/python", command[0])

    def test_subprocess_failure_is_reported_and_evidence_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video, mage_repo = self._fixture_paths(root)
            output = root / "evidence.jsonl"
            report_path = root / "report.json"
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=7,
                stdout="[t=0.0-8.0s] gate=silence (p=0.19)\n",
                stderr="dependency failure\n",
            )

            with mock.patch.object(
                run_mage_vl_streaming.subprocess,
                "run",
                return_value=completed,
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    return_code = run_mage_vl_streaming.main(
                        [
                            "--video",
                            str(video),
                            "--case-id",
                            "0704_5",
                            "--mage-repo",
                            str(mage_repo),
                            "--output",
                            str(output),
                            "--report",
                            str(report_path),
                        ]
                    )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(7, return_code)
            self.assertFalse(report["ok"])
            self.assertEqual("subprocess_failed", report["status"])
            self.assertEqual("dependency failure\n", report["raw_stderr"])
            self.assertEqual(1, report["segment_evidence_count"])

    def test_existing_output_refuses_before_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video, mage_repo = self._fixture_paths(root)
            output = root / "evidence.jsonl"
            output.write_text("owned by user\n", encoding="utf-8")
            args = run_mage_vl_streaming.parse_args(
                [
                    "--video",
                    str(video),
                    "--case",
                    "0704_5",
                    "--mage-repo",
                    str(mage_repo),
                    "--output",
                    str(output),
                    "--report",
                    str(root / "report.json"),
                ]
            )

            with mock.patch.object(
                run_mage_vl_streaming.subprocess,
                "run",
            ) as run_mock:
                with self.assertRaises(FileExistsError):
                    run_mage_vl_streaming.run(args)

            run_mock.assert_not_called()
            self.assertEqual(
                "owned by user\n",
                output.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
