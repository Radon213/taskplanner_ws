"""Deterministic, label-free image normalization for Mayo probe diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any


REENCODE_SCHEMA = "taskplanner.mayo_jpeg_reencode.v1"
LETTERBOX_SCHEMA = "taskplanner.mayo_fixed_square_letterbox.v1"
LETTERBOX_PREPROCESSOR_ID = "aspect_preserving_512_square_black_letterbox_q95"


class PixelPreprocessError(RuntimeError):
    pass


@dataclass(frozen=True)
class LetterboxResult:
    image_bytes: bytes
    metadata: dict[str, Any]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def decode_jpeg_bgr(image_bytes: bytes):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise PixelPreprocessError("OpenCV and NumPy are required for image preprocessing") from exc
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise PixelPreprocessError("cannot decode JPEG input")
    return image


def jpeg_dimensions(image_bytes: bytes) -> tuple[int, int]:
    image = decode_jpeg_bgr(image_bytes)
    height, width = image.shape[:2]
    return int(width), int(height)


def _rgb_pixel_sha256(bgr_image) -> str:
    """Hash decoded RGB pixels independently of the JPEG container bytes."""

    import numpy as np

    rgb = np.ascontiguousarray(bgr_image[:, :, ::-1])
    return sha256_bytes(rgb.tobytes())


def _jpeg_encode(image, *, jpeg_quality: int) -> tuple[bytes, dict[str, int]]:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise PixelPreprocessError("OpenCV is required for JPEG encoding") from exc
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
    encoder_flags: dict[str, int] = {"jpeg_quality": jpeg_quality}
    if hasattr(cv2, "IMWRITE_JPEG_OPTIMIZE"):
        encode_params.extend([cv2.IMWRITE_JPEG_OPTIMIZE, 0])
        encoder_flags["jpeg_optimize"] = 0
    if hasattr(cv2, "IMWRITE_JPEG_PROGRESSIVE"):
        encode_params.extend([cv2.IMWRITE_JPEG_PROGRESSIVE, 0])
        encoder_flags["jpeg_progressive"] = 0
    encoded_ok, encoded = cv2.imencode(".jpg", image, encode_params)
    if not encoded_ok:
        raise PixelPreprocessError("cannot encode JPEG")
    return bytes(encoded), encoder_flags


def deterministic_jpeg_reencode(
    image_bytes: bytes,
    *,
    jpeg_quality: int = 95,
) -> LetterboxResult:
    """Re-encode exactly the same decoded image geometry, without padding/resizing.

    OpenCV's encoder writes a new metadata-free JPEG container. JPEG Q95 is
    lossy, so source and output RGB *hashes* are reported separately; the
    invariant is that the exact decoded source raster is supplied directly to
    the encoder with no crop, resize, letterbox, or color-space conversion.
    """

    if not 1 <= jpeg_quality <= 100:
        raise PixelPreprocessError("jpeg_quality must be in [1, 100]")
    source = decode_jpeg_bgr(image_bytes)
    source_height, source_width = source.shape[:2]
    transformed, encoder_flags = _jpeg_encode(source, jpeg_quality=jpeg_quality)
    decoded_target = decode_jpeg_bgr(transformed)
    target_height, target_width = decoded_target.shape[:2]
    if (target_width, target_height) != (source_width, source_height):
        raise PixelPreprocessError("JPEG re-encode unexpectedly changed image geometry")
    metadata: dict[str, Any] = {
        "schema": REENCODE_SCHEMA,
        "transform": "deterministic_jpeg_reencode_no_resize_no_padding",
        "decoder": "opencv_imdecode_color_bgr",
        "metadata_policy": "source_container_metadata_not_copied",
        "source": {
            "mime_type": "image/jpeg",
            "byte_length": len(image_bytes),
            "sha256": sha256_bytes(image_bytes),
            "width_px": int(source_width),
            "height_px": int(source_height),
            "decoded_rgb_sha256": _rgb_pixel_sha256(source),
        },
        "target": {
            "mime_type": "image/jpeg",
            "byte_length": len(transformed),
            "sha256": sha256_bytes(transformed),
            "width_px": int(target_width),
            "height_px": int(target_height),
            "decoded_rgb_sha256": _rgb_pixel_sha256(decoded_target),
        },
        "geometry": {
            "resize_applied": False,
            "padding_applied": False,
            "crop_applied": False,
            "color_space_conversion_applied": False,
        },
        "codec": {
            "format": "jpeg",
            "encoder": "opencv_imencode",
            "flags": encoder_flags,
        },
    }
    return LetterboxResult(image_bytes=transformed, metadata=metadata)


def fixed_square_letterbox_jpeg(
    image_bytes: bytes,
    *,
    square_size: int = 512,
    jpeg_quality: int = 95,
    padding_bgr: tuple[int, int, int] = (0, 0, 0),
) -> LetterboxResult:
    """Aspect-preserving square letterbox with a deterministic JPEG encoding.

    Only pixels are transformed. No label, frame index, event, or state is
    accepted by this function. The metadata records all geometry and codec
    choices needed to reproduce the exact request bytes.
    """

    if square_size <= 0:
        raise PixelPreprocessError("square_size must be positive")
    if not 1 <= jpeg_quality <= 100:
        raise PixelPreprocessError("jpeg_quality must be in [1, 100]")
    if len(padding_bgr) != 3 or any(not 0 <= value <= 255 for value in padding_bgr):
        raise PixelPreprocessError("padding_bgr must contain three 0..255 values")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise PixelPreprocessError("OpenCV and NumPy are required for image preprocessing") from exc

    source = decode_jpeg_bgr(image_bytes)
    source_height, source_width = source.shape[:2]
    scale = min(square_size / source_width, square_size / source_height)
    resized_width = max(1, min(square_size, round(source_width * scale)))
    resized_height = max(1, min(square_size, round(source_height * scale)))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    interpolation_name = "INTER_AREA" if scale < 1.0 else "INTER_LINEAR"
    resized = cv2.resize(
        source,
        (resized_width, resized_height),
        interpolation=interpolation,
    )
    horizontal = square_size - resized_width
    vertical = square_size - resized_height
    padding = {
        "left_px": horizontal // 2,
        "right_px": horizontal - horizontal // 2,
        "top_px": vertical // 2,
        "bottom_px": vertical - vertical // 2,
    }
    canvas = np.full((square_size, square_size, 3), padding_bgr, dtype=np.uint8)
    top, left = padding["top_px"], padding["left_px"]
    canvas[top : top + resized_height, left : left + resized_width] = resized
    transformed, encoder_flags = _jpeg_encode(canvas, jpeg_quality=jpeg_quality)
    decoded_target = decode_jpeg_bgr(transformed)
    target_height, target_width = decoded_target.shape[:2]
    if (target_width, target_height) != (square_size, square_size):
        raise PixelPreprocessError("letterbox JPEG unexpectedly changed target geometry")
    metadata: dict[str, Any] = {
        "schema": LETTERBOX_SCHEMA,
        "transform": "aspect_preserving_fixed_square_letterbox",
        "decoder": "opencv_imdecode_color_bgr",
        "source": {
            "mime_type": "image/jpeg",
            "byte_length": len(image_bytes),
            "sha256": sha256_bytes(image_bytes),
            "width_px": int(source_width),
            "height_px": int(source_height),
        },
        "target": {
            "mime_type": "image/jpeg",
            "byte_length": len(transformed),
            "sha256": sha256_bytes(transformed),
            "width_px": int(target_width),
            "height_px": int(target_height),
            "decoded_rgb_sha256": _rgb_pixel_sha256(decoded_target),
        },
        "geometry": {
            "scale": scale,
            "resized_width_px": resized_width,
            "resized_height_px": resized_height,
            "padding_px": padding,
            "padding_bgr": list(padding_bgr),
            "interpolation": interpolation_name,
        },
        "codec": {
            "format": "jpeg",
            "encoder": "opencv_imencode",
            "flags": encoder_flags,
        },
    }
    return LetterboxResult(image_bytes=transformed, metadata=metadata)


def validate_fixed_square_letterbox(
    image_bytes: bytes,
    result: LetterboxResult,
    *,
    square_size: int = 512,
    jpeg_quality: int = 95,
    padding_bgr: tuple[int, int, int] = (0, 0, 0),
) -> dict[str, Any]:
    """Validate deterministic geometry and padding for one normalized image.

    This is deliberately label-free. It is used by the evaluation harness to
    record an input-integrity check for every actual request image before the
    image can reach NInfer.
    """

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise PixelPreprocessError("NumPy is required for letterbox validation") from exc

    metadata = result.metadata
    source = decode_jpeg_bgr(image_bytes)
    target = decode_jpeg_bgr(result.image_bytes)
    source_height, source_width = source.shape[:2]
    target_height, target_width = target.shape[:2]
    expected_scale = min(square_size / source_width, square_size / source_height)
    expected_width = max(1, min(square_size, round(source_width * expected_scale)))
    expected_height = max(1, min(square_size, round(source_height * expected_scale)))
    horizontal = square_size - expected_width
    vertical = square_size - expected_height
    expected_padding = {
        "left_px": horizontal // 2,
        "right_px": horizontal - horizontal // 2,
        "top_px": vertical // 2,
        "bottom_px": vertical - vertical // 2,
    }
    repeated = fixed_square_letterbox_jpeg(
        image_bytes,
        square_size=square_size,
        jpeg_quality=jpeg_quality,
        padding_bgr=padding_bgr,
    )

    # JPEG can introduce a small boundary ripple next to a black pad. Inspect
    # only the interior of each non-empty pad, excluding four boundary pixels.
    pad_margin = 4
    black_pad_values: list[int] = []
    top = expected_padding["top_px"]
    bottom = expected_padding["bottom_px"]
    left = expected_padding["left_px"]
    right = expected_padding["right_px"]
    if top > pad_margin:
        black_pad_values.append(int(target[: top - pad_margin].max()))
    if bottom > pad_margin:
        black_pad_values.append(int(target[target_height - bottom + pad_margin :].max()))
    if left > pad_margin:
        black_pad_values.append(int(target[:, : left - pad_margin].max()))
    if right > pad_margin:
        black_pad_values.append(int(target[:, target_width - right + pad_margin :].max()))
    # Baseline JPEG chroma/luma quantization can turn a nominal black pixel
    # into a tiny non-zero value even far from the resize boundary. The 10/255
    # bound catches a material pad-color regression while accepting that codec
    # artifact; the requested source canvas remains exact BGR (0, 0, 0).
    black_padding_ok = not black_pad_values or max(black_pad_values) <= 10

    checks = {
        "source_hash_matches_metadata": sha256_bytes(image_bytes) == metadata.get("source", {}).get("sha256"),
        "normalized_hash_matches_metadata": (
            sha256_bytes(result.image_bytes) == metadata.get("target", {}).get("sha256")
        ),
        "decoded_dimensions_match_metadata": (
            int(target_width) == metadata.get("target", {}).get("width_px")
            and int(target_height) == metadata.get("target", {}).get("height_px")
        ),
        "target_is_fixed_square": (target_width, target_height) == (square_size, square_size),
        "aspect_preserving_geometry": (
            metadata.get("geometry", {}).get("resized_width_px") == expected_width
            and metadata.get("geometry", {}).get("resized_height_px") == expected_height
            and metadata.get("geometry", {}).get("padding_px") == expected_padding
            and abs(float(metadata.get("geometry", {}).get("scale", -1.0)) - expected_scale) < 1e-12
        ),
        "deterministic_bytes": repeated.image_bytes == result.image_bytes,
        "black_padding_interior": black_padding_ok,
    }
    return {
        "schema": "taskplanner.mayo_letterbox_integrity.v1",
        "preprocessor": LETTERBOX_PREPROCESSOR_ID,
        "passed": all(checks.values()),
        "checks": checks,
        "expected_geometry": {
            "source_width_px": int(source_width),
            "source_height_px": int(source_height),
            "target_width_px": square_size,
            "target_height_px": square_size,
            "resized_width_px": expected_width,
            "resized_height_px": expected_height,
            "padding_px": expected_padding,
            "padding_bgr": list(padding_bgr),
        },
        "black_padding_interior_max_bgr": max(black_pad_values) if black_pad_values else 0,
    }


def letterbox_unit_contract_report() -> dict[str, Any]:
    """Run a deterministic synthetic fixture check for evaluation artifacts."""

    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise PixelPreprocessError("OpenCV and NumPy are required for letterbox unit checks") from exc
    fixture = np.zeros((154, 267, 3), dtype=np.uint8)
    fixture[:, :133] = (12, 87, 203)
    fixture[:, 133:] = (180, 41, 9)
    ok, encoded = cv2.imencode(".jpg", fixture, [cv2.IMWRITE_JPEG_QUALITY, 96])
    if not ok:
        raise PixelPreprocessError("could not encode synthetic letterbox fixture")
    source = bytes(encoded)
    first = fixed_square_letterbox_jpeg(source, square_size=512, jpeg_quality=95)
    second = fixed_square_letterbox_jpeg(source, square_size=512, jpeg_quality=95)
    validation = validate_fixed_square_letterbox(source, first, square_size=512, jpeg_quality=95)
    checks = {
        "byte_deterministic": first.image_bytes == second.image_bytes,
        "expected_267x154_to_512x295_geometry": (
            first.metadata["geometry"]["resized_width_px"] == 512
            and first.metadata["geometry"]["resized_height_px"] == 295
            and first.metadata["geometry"]["padding_px"]
            == {"left_px": 0, "right_px": 0, "top_px": 108, "bottom_px": 109}
        ),
        "runtime_integrity": bool(validation["passed"]),
    }
    return {
        "schema": "taskplanner.mayo_letterbox_unit_contract.v1",
        "preprocessor": LETTERBOX_PREPROCESSOR_ID,
        "fixture_dimensions": {"width_px": 267, "height_px": 154},
        "passed": all(checks.values()),
        "checks": checks,
        "validation": validation,
    }
