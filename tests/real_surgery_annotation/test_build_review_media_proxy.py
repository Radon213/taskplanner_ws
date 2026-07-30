from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.real_surgery_annotation.build_review_media_proxy import (
    BuildError,
    cache_matches,
    create_ffconcat,
    derive_frame_durations,
    derive_gaps_and_segments,
    quantize_durations_for_time_base,
    sha256_file,
    validate_audio_chunks,
)


class ReviewMediaProxyLogicTest(unittest.TestCase):
    def test_exact_durations_preserve_gap_and_use_corrected_final_cadence(
        self,
    ) -> None:
        timestamps = [0.0, 0.068, 0.136, 5.136, 5.205]
        durations = derive_frame_durations(timestamps, source_fps=15.0)

        for actual, expected in zip(
            durations[:-1],
            (0.068, 0.068, 5.0, 0.069),
        ):
            self.assertAlmostEqual(expected, actual)
        self.assertAlmostEqual(0.068, durations[-1])
        reconstructed = [timestamps[0]]
        for duration in durations[:-1]:
            reconstructed.append(reconstructed[-1] + duration)
        for actual, expected in zip(reconstructed, timestamps):
            self.assertAlmostEqual(expected, actual)

    def test_gap_segments_are_derived_without_case_specific_indices(
        self,
    ) -> None:
        gaps, segments = derive_gaps_and_segments(
            [0.0, 0.067, 0.134, 1.0, 1.067, 1.134],
            source_fps=15.0,
        )

        self.assertEqual(1, len(gaps))
        self.assertEqual(2, gaps[0]["before_frame_idx"])
        self.assertEqual(3, gaps[0]["after_frame_idx"])
        self.assertEqual(
            [(0, 2), (3, 5)],
            [
                (item["start_frame_idx"], item["end_frame_idx"])
                for item in segments
            ],
        )

    def test_time_base_quantization_does_not_accumulate_delta_rounding(
        self,
    ) -> None:
        timestamps = [
            0.0,
            0.068113225,
            0.136226451,
            0.204339676,
            0.272452902,
        ]
        durations = quantize_durations_for_time_base(
            timestamps,
            final_duration_sec=0.068113225,
        )
        reconstructed = [0.0]
        for duration in durations[:-1]:
            reconstructed.append(reconstructed[-1] + duration)

        for actual, expected in zip(reconstructed, timestamps):
            self.assertLessEqual(abs(actual - expected), 0.5e-6)

    def test_audio_validation_trims_only_padded_final_chunk(self) -> None:
        info = {
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "chunk_samples": 320,
            "chunk_duration_ms": 20.0,
            "samples": 641,
            "duration_sec": 641 / 16000,
        }
        result = validate_audio_chunks(
            info=info,
            audio_timestamps_ns=[0, 20_000_000, 40_000_000],
            chunk_sizes_bytes=[640, 640, 640],
        )

        self.assertEqual(319, result["trimmed_samples"])
        self.assertEqual(20_000_000, result["chunk_duration_ns"])

    def test_audio_validation_rejects_non_20ms_timestamp(self) -> None:
        info = {
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "chunk_samples": 320,
            "chunk_duration_ms": 20.0,
            "samples": 640,
            "duration_sec": 0.04,
        }
        with self.assertRaisesRegex(BuildError, "timestamp delta"):
            validate_audio_chunks(
                info=info,
                audio_timestamps_ns=[0, 20_000_001],
                chunk_sizes_bytes=[640, 640],
            )

    def test_ffconcat_has_exactly_one_entry_per_authoritative_frame(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [root / "000000.jpg", root / "000001.jpg"]
            concat = root / "frames.ffconcat"
            create_ffconcat(concat, frames, [0.1, 0.2])
            text = concat.read_text(encoding="utf-8")

        self.assertTrue(text.startswith("ffconcat version 1.0\n"))
        self.assertEqual(2, text.count("\nfile "))
        self.assertIn("duration 0.100000000", text)
        self.assertIn("duration 0.200000000", text)

    def test_cache_match_verifies_key_size_and_proxy_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "review.mp4"
            manifest = root / "review.mp4.manifest.json"
            output.write_bytes(b"proxy")
            manifest.write_text(
                json.dumps(
                    {
                        "schema": (
                            "taskplanner.review_media_proxy_manifest.v1"
                        ),
                        "cache_key_sha256": "key",
                        "output": {
                            "path": str(output.resolve()),
                            "size_bytes": output.stat().st_size,
                            "sha256": sha256_file(output),
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(
                cache_matches(
                    output_path=output,
                    manifest_path=manifest,
                    cache_key_sha256="key",
                )
            )
            output.write_bytes(b"tampered")
            self.assertFalse(
                cache_matches(
                    output_path=output,
                    manifest_path=manifest,
                    cache_key_sha256="key",
                )
            )


if __name__ == "__main__":
    unittest.main()
