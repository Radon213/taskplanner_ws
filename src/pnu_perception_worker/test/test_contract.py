from __future__ import annotations

import json

import numpy as np
import pytest
from conftest import metadata
from pnu_perception_worker.contract import ContractError, coco_rle, parse_metadata


def _raw(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def test_valid_metadata_preserves_source_exactly() -> None:
    payload = metadata(depth=True)
    parsed = parse_metadata(
        _raw(payload), depth_present=True, now_unix_ms=payload["deadline_unix_ms"] - 100
    )
    assert parsed.source == payload["source"]
    assert parsed.requested_algorithms == ("tool", "blood", "hand")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(schema="wrong"), "metadata.schema"),
        (lambda value: value.update(request_id="../escape"), "request_id"),
        (lambda value: value.update(extra="value"), "unsupported fields"),
        (
            lambda value: value.update(requested_algorithms=["tool", "tool"]),
            "unique non-empty subset",
        ),
        (
            lambda value: value["source"]["rgb"].update(frame_id="x\x00y"),
            "frame_id",
        ),
    ],
)
def test_rejects_unsafe_or_ambiguous_metadata(mutate, message: str) -> None:
    payload = metadata()
    mutate(payload)
    with pytest.raises(ContractError, match=message):
        parse_metadata(_raw(payload), depth_present=False)


def test_depth_metadata_and_binary_must_match() -> None:
    with pytest.raises(ContractError, match="appear together"):
        parse_metadata(_raw(metadata(depth=True)), depth_present=False)
    with pytest.raises(ContractError, match="appear together"):
        parse_metadata(_raw(metadata(depth=False)), depth_present=True)


def test_rgb_depth_skew_is_bounded_to_live_sync_contract() -> None:
    payload = metadata(depth=True)
    payload["source"]["depth"]["stamp_ns"] = (
        payload["source"]["rgb"]["stamp_ns"] - 50_000_001
    )
    with pytest.raises(ContractError, match="stamp skew"):
        parse_metadata(_raw(payload), depth_present=True)


def test_deadline_is_bounded_and_must_be_fresh() -> None:
    payload = metadata()
    deadline = payload["deadline_unix_ms"]
    with pytest.raises(ContractError, match="expired"):
        parse_metadata(_raw(payload), depth_present=False, now_unix_ms=deadline)
    with pytest.raises(ContractError, match="too far"):
        parse_metadata(
            _raw(payload),
            depth_present=False,
            now_unix_ms=deadline - 20_000,
            max_deadline_ahead_ms=10_000,
        )


def test_camera_info_has_fixed_matrix_shapes() -> None:
    payload = metadata()
    payload["color_camera_info"] = {
        "stamp_ns": payload["source"]["rgb"]["stamp_ns"],
        "frame_id": "cam_4_color_optical_frame",
        "width": 1280,
        "height": 720,
        "distortion_model": "plumb_bob",
        "d": [0.0] * 5,
        "k": [1.0] * 8,
        "r": [1.0] * 9,
        "p": [1.0] * 12,
    }
    with pytest.raises(ContractError, match="k must contain 9"):
        parse_metadata(_raw(payload), depth_present=False)


@pytest.mark.parametrize(
    ("model", "distortion"),
    [
        ("plumb_bob", [0.0]),
        ("equidistant", [0.0] * 4),
        ("rational_polynomial", [0.0] * 8),
    ],
)
def test_camera_info_rejects_unsupported_opencv_distortion_contract(
    model, distortion
) -> None:
    payload = metadata()
    payload["color_camera_info"] = {
        "stamp_ns": payload["source"]["rgb"]["stamp_ns"],
        "frame_id": "cam_4_color_optical_frame",
        "width": 1280,
        "height": 720,
        "distortion_model": model,
        "d": distortion,
        "k": [100.0, 0.0, 16.0, 0.0, 100.0, 12.0, 0.0, 0.0, 1.0],
        "r": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "p": [100.0, 0.0, 16.0, 0.0, 0.0, 100.0, 12.0, 0.0, 0.0, 0.0, 1.0, 0.0],
    }
    with pytest.raises(ContractError, match="plumb_bob distortion with 5"):
        parse_metadata(_raw(payload), depth_present=False)


def test_coco_rle_is_column_major_and_json_safe() -> None:
    mask = np.array([[False, True], [True, True]], dtype=bool)
    encoded = coco_rle(mask)
    assert encoded == {"size": [2, 2], "counts": [1, 3]}
    json.dumps(encoded)


def test_vectorized_coco_rle_matches_reference_for_random_masks() -> None:
    rng = np.random.default_rng(42)

    def reference(mask):
        counts = []
        previous = 0
        run = 0
        for pixel in np.asarray(mask, dtype=np.uint8).reshape(-1, order="F"):
            current = int(pixel != 0)
            if current == previous:
                run += 1
            else:
                counts.append(run)
                previous = current
                run = 1
        counts.append(run)
        return counts

    masks = [
        np.zeros((720, 1280), dtype=bool),
        np.ones((17, 13), dtype=bool),
        np.asarray([[0, 2], [-1, 0]], dtype=np.int8),
        rng.random((31, 47)) > 0.7,
    ]
    for mask in masks:
        assert coco_rle(mask)["counts"] == reference(mask)


def test_coco_rle_fails_closed_before_unbounded_output() -> None:
    mask = np.array([[False, True], [True, False]], dtype=bool)
    with pytest.raises(ValueError, match="count limit"):
        coco_rle(mask, max_counts=2)
