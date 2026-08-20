from __future__ import annotations

import cv2
import numpy as np

import mayo_pixel_preprocess as pixels


def _synthetic_jpeg(width: int = 267, height: int = 154) -> bytes:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, : width // 2] = (20, 80, 200)
    image[:, width // 2 :] = (180, 40, 10)
    image[10 : height - 10, 10 : width - 10] = (90, 200, 30)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert ok
    return bytes(encoded)


def test_reencode_q95_is_byte_deterministic_and_preserves_geometry_without_padding():
    source = _synthetic_jpeg()
    first = pixels.deterministic_jpeg_reencode(source, jpeg_quality=95)
    second = pixels.deterministic_jpeg_reencode(source, jpeg_quality=95)
    assert first.image_bytes == second.image_bytes
    metadata = first.metadata
    assert metadata["transform"] == "deterministic_jpeg_reencode_no_resize_no_padding"
    assert metadata["source"]["width_px"] == 267
    assert metadata["source"]["height_px"] == 154
    assert metadata["target"]["width_px"] == 267
    assert metadata["target"]["height_px"] == 154
    assert metadata["geometry"] == {
        "resize_applied": False,
        "padding_applied": False,
        "crop_applied": False,
        "color_space_conversion_applied": False,
    }
    assert pixels.jpeg_dimensions(first.image_bytes) == (267, 154)


def test_letterbox_512_geometry_and_padding_are_exact_and_deterministic():
    source = _synthetic_jpeg()
    first = pixels.fixed_square_letterbox_jpeg(source, square_size=512, jpeg_quality=95)
    second = pixels.fixed_square_letterbox_jpeg(source, square_size=512, jpeg_quality=95)
    assert first.image_bytes == second.image_bytes
    metadata = first.metadata
    assert metadata["target"]["width_px"] == 512
    assert metadata["target"]["height_px"] == 512
    assert metadata["geometry"]["resized_width_px"] == 512
    assert metadata["geometry"]["resized_height_px"] == 295
    assert metadata["geometry"]["padding_px"] == {
        "left_px": 0,
        "right_px": 0,
        "top_px": 108,
        "bottom_px": 109,
    }
    assert metadata["geometry"]["padding_bgr"] == [0, 0, 0]
    assert pixels.jpeg_dimensions(first.image_bytes) == (512, 512)
    assert metadata["target"]["decoded_rgb_sha256"]


def test_letterbox_runtime_integrity_reports_hash_geometry_and_black_padding_checks():
    source = _synthetic_jpeg()
    result = pixels.fixed_square_letterbox_jpeg(source, square_size=512, jpeg_quality=95)
    validation = pixels.validate_fixed_square_letterbox(source, result, square_size=512, jpeg_quality=95)
    assert validation["passed"] is True
    assert all(validation["checks"].values())
    assert validation["expected_geometry"]["padding_px"] == {
        "left_px": 0,
        "right_px": 0,
        "top_px": 108,
        "bottom_px": 109,
    }


def test_letterbox_unit_contract_fixture_is_self_consistent():
    report = pixels.letterbox_unit_contract_report()
    assert report["passed"] is True
    assert all(report["checks"].values())
