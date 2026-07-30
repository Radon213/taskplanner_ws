from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from tools.real_surgery_annotation.render_interaction_contact_sheet import (
    FRAME_HEIGHT,
    GUTTER,
    HEADER_HEIGHT,
    SHEET_MARGIN,
    TILE_WIDTH,
    ContactSheetError,
    Timeline,
    ViewSpec,
    decode_exact_frames,
    nearest_frame_index,
    render_contact_sheet,
    select_sample_plan,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = (
    WORKSPACE_ROOT
    / "tools"
    / "real_surgery_annotation"
    / "render_interaction_contact_sheet.py"
)
TEST_PATH = Path(__file__).resolve()


def timeline_payload(
    timestamps: list[float],
    *,
    case_id: str = "0704_7",
    source_fps: float = 10.0,
) -> dict:
    gaps = [
        {
            "before_frame_idx": index,
            "after_frame_idx": index + 1,
            "before_time_sec": left,
            "after_time_sec": right,
            "delta_sec": right - left,
        }
        for index, (left, right) in enumerate(
            zip(timestamps, timestamps[1:])
        )
        if right - left > 1.5 / source_fps
    ]
    return {
        "schema": "taskplanner.video_frame_timeline.v1",
        "case_id": case_id,
        "source_bag": "/read-only/source",
        "topic": "/surgery/cam4/color/image/compressed",
        "timeline_origin": "first_topic_message",
        "source_fps": source_fps,
        "frame_count": len(timestamps),
        "start_sec": timestamps[0],
        "end_sec": timestamps[-1],
        "gaps": gaps,
        "timestamps_sec": timestamps,
    }


def write_timeline(path: Path, timestamps: list[float]) -> None:
    path.write_text(
        json.dumps(timeline_payload(timestamps)),
        encoding="utf-8",
    )


def frame_bgr(frame_index: int, view_index: int) -> tuple[int, int, int]:
    return (
        20 + frame_index * 18,
        45 + view_index * 70,
        190 - frame_index * 10,
    )


def write_test_avi(
    path: Path,
    *,
    frame_count: int,
    view_index: int,
) -> None:
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        10.0,
        (96, 72),
    )
    if not writer.isOpened():
        raise RuntimeError("test environment cannot create MJPG AVI")
    try:
        for frame_index in range(frame_count):
            color = frame_bgr(frame_index, view_index)
            frame = np.full((72, 96, 3), color, dtype=np.uint8)
            writer.write(frame)
    finally:
        writer.release()


class SampleSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.timeline = Timeline(
            case_id="0704_7",
            source_fps=10.0,
            timestamps_sec=(0.0, 0.1, 0.2, 1.0, 1.1),
        )

    def test_corrected_bag_time_maps_to_nearest_frame_with_earlier_tie(
        self,
    ) -> None:
        self.assertEqual(
            1,
            nearest_frame_index(self.timeline.timestamps_sec, 0.15),
        )
        self.assertEqual(
            3,
            nearest_frame_index(self.timeline.timestamps_sec, 0.96),
        )

        plan = select_sample_plan(
            self.timeline,
            center_bag_sec=0.96,
            before_sec=0.06,
            after_sec=0.14,
            step_sec=0.05,
        )

        self.assertEqual(3, plan.center_frame)
        self.assertEqual((3, 4), plan.frame_indices)
        self.assertEqual("corrected_bag_time_window", plan.selection_mode)

    def test_explicit_frame_window_is_inclusive_of_requested_last_frame(
        self,
    ) -> None:
        plan = select_sample_plan(
            self.timeline,
            first_frame=0,
            last_frame=4,
            frame_step=3,
        )

        self.assertEqual((0, 3, 4), plan.frame_indices)
        self.assertIsNone(plan.center_frame)

    def test_invalid_argument_combinations_fail_closed(self) -> None:
        cases = (
            {
                "center_frame": 1,
                "center_bag_sec": 0.1,
                "before_sec": 0.1,
                "after_sec": 0.1,
                "step_sec": 0.1,
            },
            {
                "center_frame": 1,
                "before_sec": 0.1,
                "after_sec": 0.1,
            },
            {
                "center_frame": 1,
                "before_sec": 0.1,
                "after_sec": 0.1,
                "step_sec": 0.0,
            },
            {
                "first_frame": 0,
                "last_frame": 4,
            },
            {
                "first_frame": 4,
                "last_frame": 0,
                "frame_step": 1,
            },
            {
                "first_frame": 0,
                "last_frame": 4,
                "frame_step": 0,
            },
            {
                "center_bag_sec": 2.0,
                "before_sec": 0.1,
                "after_sec": 0.1,
                "step_sec": 0.1,
            },
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ContactSheetError):
                    select_sample_plan(self.timeline, **arguments)


class ExactDecodeAndRenderTest(unittest.TestCase):
    def test_multi_view_sheet_uses_the_same_exact_frame_indices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timeline = root / "timeline.json"
            cam4 = root / "cam4.avi"
            flir = root / "flir.avi"
            output = root / "packet.png"
            write_timeline(timeline, [0.0, 0.1, 0.2, 0.3, 0.4, 0.5])
            write_test_avi(cam4, frame_count=6, view_index=0)
            write_test_avi(flir, frame_count=6, view_index=1)

            summary = render_contact_sheet(
                case_id="0704_7",
                timeline_path=timeline,
                view_values=[f"cam4={cam4}", f"flir={flir}"],
                first_frame=2,
                last_frame=4,
                frame_step=2,
                output=output,
            )

            self.assertEqual([2, 4], summary["sample_frame_indices"])
            self.assertEqual([0.2, 0.4], summary["sample_bag_times_sec"])
            self.assertTrue(all(item["seek_verified"] for item in summary["views"]))
            self.assertTrue(output.is_file())

            with Image.open(output) as sheet:
                expected_width = SHEET_MARGIN * 2 + 2 * TILE_WIDTH + GUTTER
                self.assertEqual(expected_width, sheet.width)
                tile_height = FRAME_HEIGHT + 44
                expected_height = (
                    HEADER_HEIGHT
                    + SHEET_MARGIN
                    + 2 * tile_height
                    + GUTTER
                    + SHEET_MARGIN
                )
                self.assertEqual(expected_height, sheet.height)

                for column, view_index in enumerate((0, 1)):
                    x = (
                        SHEET_MARGIN
                        + column * (TILE_WIDTH + GUTTER)
                        + TILE_WIDTH // 2
                    )
                    y = HEADER_HEIGHT + SHEET_MARGIN + FRAME_HEIGHT // 2
                    actual_rgb = sheet.getpixel((x, y))
                    expected_bgr = frame_bgr(2, view_index)
                    expected_rgb = (
                        expected_bgr[2],
                        expected_bgr[1],
                        expected_bgr[0],
                    )
                    for actual, expected in zip(actual_rgb, expected_rgb):
                        self.assertLessEqual(abs(actual - expected), 12)

    def test_short_view_fails_without_publishing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timeline = root / "timeline.json"
            short_view = root / "short.avi"
            output = root / "packet.png"
            write_timeline(timeline, [0.0, 0.1, 0.2, 0.3, 0.4])
            write_test_avi(short_view, frame_count=3, view_index=0)

            with self.assertRaisesRegex(ContactSheetError, "has 3 frames"):
                render_contact_sheet(
                    case_id="0704_7",
                    timeline_path=timeline,
                    view_values=[f"cam4={short_view}"],
                    first_frame=2,
                    last_frame=4,
                    frame_step=1,
                    output=output,
                )

            self.assertFalse(output.exists())

    def test_existing_output_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timeline = root / "timeline.json"
            view = root / "cam4.avi"
            output = root / "packet.png"
            write_timeline(timeline, [0.0, 0.1])
            write_test_avi(view, frame_count=2, view_index=0)
            output.write_bytes(b"sentinel")

            with self.assertRaisesRegex(ContactSheetError, "refusing to overwrite"):
                render_contact_sheet(
                    case_id="0704_7",
                    timeline_path=timeline,
                    view_values=[f"cam4={view}"],
                    first_frame=0,
                    last_frame=1,
                    frame_step=1,
                    output=output,
                )

            self.assertEqual(b"sentinel", output.read_bytes())

    def test_decoder_position_drift_is_rejected(self) -> None:
        class FakeCapture:
            def __init__(self) -> None:
                self.position = 0.0

            def isOpened(self) -> bool:
                return True

            def get(self, property_id: int) -> float:
                if property_id == FakeCV2.CAP_PROP_FRAME_COUNT:
                    return 10.0
                if property_id == FakeCV2.CAP_PROP_POS_FRAMES:
                    return self.position + 1.0
                raise AssertionError(property_id)

            def set(self, property_id: int, value: float) -> bool:
                self.position = value
                return property_id == FakeCV2.CAP_PROP_POS_FRAMES

            def read(self):
                raise AssertionError("read must not run after failed seek verification")

            def release(self) -> None:
                return None

        class FakeCV2:
            CAP_PROP_FRAME_COUNT = 1
            CAP_PROP_POS_FRAMES = 2

            @staticmethod
            def VideoCapture(_path: str) -> FakeCapture:
                return FakeCapture()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dummy.avi"
            path.write_bytes(b"not-decoded")
            with self.assertRaisesRegex(
                ContactSheetError,
                "exact seek verification failed",
            ):
                decode_exact_frames(
                    ViewSpec(label="cam4", path=path),
                    [3],
                    cv2_module=FakeCV2,
                )


class CommandLineCompatibilityTests(unittest.TestCase):
    def test_help_works_for_direct_script_and_module_execution(self) -> None:
        commands = (
            [sys.executable, str(TOOL_PATH), "--help"],
            [
                sys.executable,
                "-m",
                "tools.real_surgery_annotation.render_interaction_contact_sheet",
                "--help",
            ],
        )

        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run(
                    command,
                    cwd=WORKSPACE_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("--view LABEL=AVI", result.stdout)

    def test_new_source_files_have_mode_0644(self) -> None:
        self.assertEqual(0o644, TOOL_PATH.stat().st_mode & 0o777)
        self.assertEqual(0o644, TEST_PATH.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
