from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.real_surgery_annotation.build_review_media_proxy import (
    AudioExtraction,
    BuildError,
    BuildOptions,
    TimelineSpec,
    sha256_file,
)
from tools.real_surgery_annotation.build_review_multiview_media import (
    MANIFEST_SCHEMA,
    OUTPUT_FILENAMES,
    VIEW_ORDER,
    independent_proxy_encoding_command,
    multiview_cache_matches,
    probe_independent_output,
    single_view_frame_extraction_command,
)


def _timeline() -> TimelineSpec:
    return TimelineSpec(
        case_id="0704_6",
        topic="/surgery/camera4/image",
        source_bag=Path("/bag"),
        source_fps=15.0,
        timestamps_sec=(0.0, 0.068, 0.136),
        frame_durations_sec=(0.068, 0.068, 0.068),
        gaps=(),
        segments=(),
    )


def _audio() -> AudioExtraction:
    return AudioExtraction(
        pcm_bytes=b"\x00\x00" * 320,
        sample_rate=16000,
        channels=1,
        sample_count=320,
        duration_sec=0.02,
        start_sec=0.0,
        chunk_samples=320,
        chunk_duration_ns=20_000_000,
        chunk_count=1,
        trimmed_samples=0,
        info={},
    )


def _options() -> BuildOptions:
    return BuildOptions(
        panel_width=640,
        panel_height=360,
        jpeg_quality=3,
        video_encoder="libx264",
        preset="veryfast",
        crf=20,
        gop_frames=30,
        audio_bitrate="128k",
        audio_info_topic="/audio/info",
        audio_pcm_topic="/audio/pcm",
    )


class ReviewMultiviewMediaLogicTest(unittest.TestCase):
    def test_extraction_uses_exactly_one_source_and_never_composites(self) -> None:
        command = single_view_frame_extraction_command(
            ffmpeg="/usr/bin/ffmpeg",
            source_avi=Path("/source/cam_2/rgb.avi"),
            frames_pattern=Path("/tmp/frames/%06d.jpg"),
            timeline=_timeline(),
            options=_options(),
        )
        rendered = " ".join(command)

        self.assertEqual(1, command.count("-i"))
        self.assertIn("/source/cam_2/rgb.avi", command)
        self.assertIn("scale=640:360", rendered)
        self.assertNotIn("hstack", rendered)
        self.assertNotIn("xstack", rendered)

    def test_only_master_encoding_command_muxes_audio(self) -> None:
        common = {
            "ffmpeg": "/usr/bin/ffmpeg",
            "concat_path": Path("/tmp/frames.ffconcat"),
            "wav_path": Path("/tmp/audio.wav"),
            "timeline": _timeline(),
            "audio": _audio(),
            "options": _options(),
        }
        master = independent_proxy_encoding_command(
            **common,
            output_path=Path("/tmp/review_cam4.mp4"),
            include_audio=True,
        )
        follower = independent_proxy_encoding_command(
            **common,
            output_path=Path("/tmp/review_cam2.mp4"),
            include_audio=False,
        )

        self.assertIn("/tmp/audio.wav", master)
        self.assertIn("1:a:0", master)
        self.assertNotIn("-an", master)
        self.assertNotIn("/tmp/audio.wav", follower)
        self.assertNotIn("1:a:0", follower)
        self.assertIn("-an", follower)
        self.assertIn(str(1_000_000), follower)

    def test_bundle_cache_requires_every_output_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_paths = {
                view: root / OUTPUT_FILENAMES[view] for view in VIEW_ORDER
            }
            for view, path in output_paths.items():
                path.write_bytes(f"proxy-{view}".encode())
            manifest_path = root / "review_multiview.manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": MANIFEST_SCHEMA,
                        "cache_key_sha256": "key",
                        "master_view": "cam4",
                        "view_order": list(VIEW_ORDER),
                        "outputs": {
                            view: {
                                "path": str(path.resolve()),
                                "size_bytes": path.stat().st_size,
                                "sha256": sha256_file(path),
                            }
                            for view, path in output_paths.items()
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(
                multiview_cache_matches(
                    output_paths=output_paths,
                    manifest_path=manifest_path,
                    cache_key_sha256="key",
                )
            )
            output_paths["cam3"].write_bytes(b"tampered")
            self.assertFalse(
                multiview_cache_matches(
                    output_paths=output_paths,
                    manifest_path=manifest_path,
                    cache_key_sha256="key",
                )
            )

    def test_probe_accepts_mp4_duration_rounding_but_rejects_truncation(
        self,
    ) -> None:
        stream_value = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": 640,
                    "height": 360,
                }
            ],
            # A muxer may round format duration a few milliseconds before the
            # exact final frame PTS even though all frames are present.
            "format": {"duration": "0.1345"},
        }
        frame_value = {
            "frames": [
                {"best_effort_timestamp_time": "0.000000"},
                {"best_effort_timestamp_time": "0.068000"},
                {"best_effort_timestamp_time": "0.136000"},
            ]
        }
        with patch(
            "tools.real_surgery_annotation.build_review_multiview_media."
            "base.run_json",
            side_effect=[stream_value, frame_value],
        ):
            probe = probe_independent_output(
                ffprobe="/usr/bin/ffprobe",
                proxy_path=Path("/tmp/review_cam2.mp4"),
                timeline=_timeline(),
                audio=_audio(),
                options=_options(),
                include_audio=False,
            )
        self.assertEqual(3, probe["frame_count"])

        truncated_stream = {
            **stream_value,
            "format": {"duration": "0.010"},
        }
        with (
            patch(
                "tools.real_surgery_annotation.build_review_multiview_media."
                "base.run_json",
                side_effect=[truncated_stream, frame_value],
            ),
            self.assertRaises(BuildError),
        ):
            probe_independent_output(
                ffprobe="/usr/bin/ffprobe",
                proxy_path=Path("/tmp/review_cam2.mp4"),
                timeline=_timeline(),
                audio=_audio(),
                options=_options(),
                include_audio=False,
            )


if __name__ == "__main__":
    unittest.main()
